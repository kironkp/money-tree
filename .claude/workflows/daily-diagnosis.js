export const meta = {
  name: 'daily-diagnosis',
  description: 'Morning check on the MoneyTree desk: what broke quietly, what it cost, what to do',
  whenToUse: 'Run every morning before the open, or on demand when something looks wrong.',
  phases: [{ title: 'Diagnose', detail: 'seven fixed angles over the live database' }],
}

const REPO = '/Users/kironkp/code/moneytree'

/* The discipline that keeps this from manufacturing work.
 *
 * A daily job told to "find improvements" will find some every day, because it is
 * being asked to. That produces a stream of plausible suggestions, an owner who
 * stops reading them, and a system tuned to whatever the last report said — which
 * is overfitting with extra steps.
 *
 * So six of the seven agents check a FIXED list of known failure modes and report
 * what CHANGED. "Nothing new" is a complete and welcome answer, and an agent that
 * says it has done its job. Only the seventh is open-ended.
 */
const RULES = `
You are diagnosing a live automated trading desk. Read-only: no Edit/Write, no migrations, no
mutating commands, no restarts. You may run SELECT-only Django shell queries
(\`pipenv run python manage.py shell -c "..."\`), read any file, and read logs under run/.

Report ONLY what changed since yesterday or is genuinely anomalous. If nothing did, say
"nothing new" and stop — that is a complete answer and the most common correct one. Do not pad,
do not restate yesterday's known problems as though they were discoveries, and do not propose
improvements that are really just preferences.

Every number you state must come from a query you actually ran. Quote the number. If you cannot
verify something, say so instead of estimating. Web search may be unavailable; work from the
database and the filesystem.

Severity, and be strict about it:
  critical - money is being lost or risked right now by something broken
  warn     - something is degrading, or a safeguard is not working
  note     - worth knowing, no action needed today
`

const CONTEXT = `
MoneyTree at ${REPO}: a Django autonomous day-trading desk trading FAKE money across four lanes
(stocks, crypto, degen, forex). Read ${REPO}/CLAUDE.md first — it is dense, accurate, and its
"Invariants that matter" section lists failure modes that have already happened here.

Known history worth not rediscovering: the news arm once vetoed every signal for a day because a
fix sat on disk while the agents ran older code; the watchdog's schedule file existed for weeks
without ever being loaded; SIP bar history went 16 days stale while the dashboard looked current;
one PEPE bar sat at 1,248x the true price for nine months. All four were silent. None produced an
error. That is the class of thing this job exists to catch.
`

const FINDINGS = {
  type: 'object', additionalProperties: false,
  required: ['headline', 'findings'],
  properties: {
    headline: { type: 'string', description: 'one sentence; "nothing new" is valid and common' },
    findings: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['severity', 'what', 'evidence', 'action'],
        properties: {
          severity: { type: 'string', enum: ['critical', 'warn', 'note'] },
          what: { type: 'string', description: 'the problem, in one plain sentence' },
          evidence: { type: 'string', description: 'the query you ran and the number it returned' },
          action: { type: 'string', description: 'the smallest thing that would fix it, or "none"' },
        },
      },
    },
  },
}

const AGENTS = [
  {
    key: 'silent-failures',
    title: 'Is everything actually running?',
    prompt: `The most expensive failures on this desk have all been silent. Check, with evidence:
 - Are all four lane agents alive? (AgentRun status/health, and the pids in run/agent-sim-*.log.)
   When did each START? Compare that against the mtime of files under main_app/services/ and
   strategies/ — a lane running code older than the last edit is running a fix that has not landed.
 - Which launchd jobs are actually loaded (\`launchctl list | grep moneytree\`) and when did each
   last write its log under run/? A plist that exists but never fires is the watchdog failure again.
 - Is the web server up? Is the Tailscale funnel still serving (\`tailscale funnel status\`)?
 - Did CI pass on the latest commit (\`gh run list --repo kironkp/money-tree --limit 3\`)?
 - Is the working tree clean and is main pushed? Unpushed work is unbacked-up work.
 - Any process holding a stale lock in run/?`,
  },
  {
    key: 'money',
    title: 'What did each bot make and what did it cost?',
    prompt: `Per lane, yesterday and against the trailing 7 and 30 days. Use report.lane_costs()
and the Trade table directly.
 - P&L, trade count, money MOVED (notional), fees, fees in bps, and net BEFORE fees. Per lane,
   never summed — the four lanes have wildly different economics and a blended number hides it.
 - What changed materially versus the trailing average? A lane whose turnover or fee rate moved a
   lot is more interesting than one that simply lost money again.
 - Any lane profitable before fees but negative after? That is the single most important signal on
   this desk and it should be called out every time it is true.
 - Exit-reason mix per lane (target/stop/timeout). A lane dominated by timeouts is paying full
   round trips for trades that went nowhere.
 - Open positions carried overnight and their unrealised P&L.`,
  },
  {
    key: 'data',
    title: 'Is the data honest?',
    prompt: `Everything downstream is sized from these bars.
 - Feed freshness per instrument, timeframe and SOURCE. Flag anything a live lane reads that is
   more than a day stale, and anything a BACKTEST reads (SIP is preferred by best_source) more than
   a week stale. This has silently broken the nightly research before.
 - Run the quality gate's own checks over recent bars: impossible prices (see OUTLIER_FACTOR in
   services/data/store.py), bad OHLC, duplicate timestamps, gaps over 3 bars, overnight jumps
   above 30% that might be unhandled splits.
 - Compare bar counts per lane against what a full session should contain. Missing bars are as
   damaging as wrong ones and much harder to notice.
 - Is the news pipeline ingesting? Stories stored, revised, and discarded in the last 24h, and how
   many distinct tickers landed in UnmatchedSymbol.`,
  },
  {
    key: 'risk',
    title: 'What got refused, and is any lane gated shut?',
    prompt: `A lane can look alive while being arithmetically incapable of trading.
 - Signal.blocked_reason counts for the last 24h and 7 days, grouped and ranked, per lane.
 - Is any strategy emitting ONLY blocked signals? burst was doing exactly that — its 1.50% target
   could not clear 3x the 0.56% crypto round trip — while appearing to run normally.
 - Any lane halted (Account.day_halted) and why. Any news-arm limit tripped (services/news_risk.py).
 - Stuck instruction leases: NewsVerdict rows in lease_state='leased' past lease_expires_at.
 - Are the preregistered risk limits still at their frozen values? Changing one silently
   invalidates the running evaluation.`,
  },
  {
    key: 'evidence',
    title: 'Is the learning machinery actually learning?',
    prompt: ` - The open Evaluation: days collected, next checkpoint, delta_min, current long-run sigma
   and required days. Is it accumulating, and is the fingerprint unchanged?
 - Dossiers written, their scores, and how many produced a tradable catalyst. If every dossier
   concludes "no trade", the research column of the experiment is structurally all zeros and the
   primary hypothesis cannot be measured — say so loudly.
 - Verdict grading: how many graded contemporaneously, hit rate, calibration of p_target_first.
   Never pool reconstructed rows with contemporaneous ones.
 - Last night's auto_research: what did it try, what did it promote, what did it reject and why.
 - Is any strategy DUE for quarantine under promotion.qualification_assessment or
   promotion.lifetime_verdict but still showing enabled? A brake that is due and has not fired is
   the most expensive bug this codebase has had.`,
  },
  {
    key: 'spend',
    title: 'What did the machine cost to run?',
    prompt: ` - ApiUsage for yesterday and the trailing 30 days, grouped by purpose and by model.
   Compare against AgentConfig.research_budget_usd_per_day and the module ceilings.
 - Any reservation left in state='reserved' or 'failed' — those are calls that cost money and
   produced nothing, and a pile of them means something is retrying.
 - Any model priced at spend.DEFAULT_PRICE, which means it is missing from the price table and the
   recorded cost is a guess.
 - Is the recorded cost plausible against the work done? The ledger has understated itself before:
   a hardcoded price 2.15x low, and a web-search fee that never reached the table at all.
 - Projected monthly run rate, and whether it is rising.`,
  },
  {
    key: 'open',
    title: 'What did the other six miss?',
    prompt: `The other six agents check a fixed list. Your job is everything not on it.

Go looking for the most expensive thing that is wrong today that a checklist would not find. Read
recent code changes (\`git log --oneline -20\`, \`git diff HEAD~3 --stat\`), the newest entries in
run/*.log, recent FeedEvent rows at level='error', JournalEntry, and anything in the codebase that
looks inconsistent with what CLAUDE.md claims.

Good instincts: a number on a screen that cannot be what it says; a safeguard whose test would
pass even if it were deleted; a value that is computed one way in one file and another way
elsewhere; a comment that no longer matches its code; a recently changed file whose tests did not
change with it.

If you find nothing, say so. A confident "nothing new" from this agent is worth more than a list
of things that might be worth looking at.`,
  },
]

phase('Diagnose')
log(`Morning check: ${AGENTS.length} angles over the live desk`)

const results = (await parallel(AGENTS.map(a => () =>
  agent(`${RULES}\n${CONTEXT}\n\nYOUR ANGLE — ${a.title}\n\n${a.prompt}`,
    { label: a.key, phase: 'Diagnose', schema: FINDINGS })
    .then(r => ({ key: a.key, title: a.title, ...r }))
))).filter(Boolean)

const all = results.flatMap(r => (r.findings || []).map(f => ({ ...f, angle: r.key })))
const rank = { critical: 0, warn: 1, note: 2 }
all.sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3))

return {
  headlines: results.map(r => ({ angle: r.key, title: r.title, headline: r.headline })),
  findings: all,
  counts: {
    critical: all.filter(f => f.severity === 'critical').length,
    warn: all.filter(f => f.severity === 'warn').length,
    note: all.filter(f => f.severity === 'note').length,
  },
}
