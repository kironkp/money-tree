# MoneyTree 🌳

A day-trading agent that grows from **seed capital** (fake currency) into a
real account without a rewrite. Django 6.1, one app, SQLite in dev.

The ladder every strategy climbs:

| Stage | What trades | Fills |
|---|---|---|
| **Seed** | backtests only | simulator, next-bar open |
| **Sprout** | fake $10,000 on the internal simulator, real quotes | simulator, last price ± slippage |
| **Sapling** | Alpaca **paper** account | Alpaca (paper) |
| **Tree** | real money (v2) | Alpaca (live), double interlock |

Same engine, same risk manager, same strategy code at every stage. Promotion
is gated by a checklist (sessions, trades, profit factor, drawdown, live
expectancy vs backtest, realized slippage) shown on each strategy page.
Version 1.31 separates **enabled for observation** from **statistically
qualified**: unproven ideas may collect fake-money evidence, but paper/live
agents refuse them. A measured losing strategy is quarantined and disabled.
Walk-forward promotion also requires both a profitable final holdout and a
profitable adaptive selection pipeline; the optimizer cannot nominate the
least-bad result when every training candidate loses after costs.

Four lanes, four piggy banks: **Stocks** (NYSE/Nasdaq session), **Crypto**
(BTC/ETH, hourly, careful), **Degen** (Alpaca altcoins on 1-minute bars,
aggressive sizing, its own loose limits — the high-risk sandbox, clearly
labeled, expected to bleed) and **Forex** (EUR/GBP/AUD/NZD against the dollar
on 15-minute bars, Sunday 17:00 to Friday 17:00 ET, traded on 10× margin like
a retail forex account, Yahoo quotes, simulator only until a forex broker
adapter exists). Every lane has a live **pulse** every 10 seconds
between bar decisions: prices, open P&L, distance to stop/target and to the
nearest trigger, streamed into the decision journal.

Two of them in detail: the **stocks agent** trades the NYSE/Nasdaq
session (09:30–16:00 ET, sleeps otherwise) and the **crypto agent** trades
BTC/USD and ETH/USD around the clock. Each has its own account, strategy
rows, process and **live feed** — a running commentary of what it sees,
decides and does, streamed onto the dashboard.

## Run it

```bash
pipenv install
cp .env.example .env            # fill DJANGO_SUPERUSER_*, optionally Alpaca keys
pipenv run python manage.py migrate
pipenv run python manage.py bootstrap_admin
pipenv run python manage.py seed_watchlist
pipenv run python manage.py sync_bars --days 60          # alpaca if keyed, else yahoo
pipenv run python manage.py runserver 0.0.0.0:8003
```

Then, in a second terminal, the agent:

```bash
pipenv run python manage.py run_agent --mode sim --market stocks   # fake currency, live quotes
pipenv run python manage.py run_agent --mode sim --market crypto   # BTC/ETH, hourly
pipenv run python manage.py run_agent --mode sim --market degen    # altcoins, 1-minute bars, the sandbox
pipenv run python manage.py run_agent --mode sim --market forex    # majors, 15-minute bars, 24/5, on margin
pipenv run python manage.py run_agent --replay 2026-08-28 --speed 30 --market stocks   # demo a past session
```

Backups run nightly at 02:00 (launchd job `com.kiron.moneytree.gitpush`).
`deploy/git-push.sh` refreshes the trading-data backup, commits anything
uncommitted, and pushes code and tags to the private `kironkp/money-tree` repo.
`deploy/backup-data.sh` writes `data-backup/ledger.sql` — the schema plus every
trade, order, fill, position, equity snapshot and journal entry, about 1.7 MB
of SQL that git versions night by night — and on Sundays uploads the whole
compressed database as a release asset under the `db-snapshot` tag.

To restore on a new machine:

```bash
git clone https://github.com/kironkp/money-tree.git moneytree && cd moneytree
sqlite3 db.sqlite3 < data-backup/ledger.sql   # money and decisions, no bars
pipenv install && pipenv run python manage.py bootstrap_admin
pipenv run python manage.py sync_bars --days 60        # refetch the bars
```

Or take the whole database, bars included, from the latest release:
`gh release download db-snapshot --repo kironkp/money-tree` then
`gunzip -c moneytree-db-latest.sqlite3.gz > db.sqlite3`.

Or start/stop it from the dashboard. One agent per account (file lock in
`run/`). Ctrl-C / SIGTERM flattens sim and paper positions on the way out.

No Alpaca keys? `sync_bars --provider synthetic` gives you a seeded random
walk to exercise everything; `--provider yahoo` gives real bars (60 days of
5-min, 7 days of 1-min, rate-limited). With free Alpaca paper keys you get
years of split-adjusted history and real-time IEX quotes.

## What the screen answers in ten seconds

The status strip says whether this is real money, whether the agent is healthy,
waiting, stale or disconnected, how old the last completed bar is, when the books
were last reconciled with the broker, whether trading is on and the kill switch
off, and **"Safe to trade now: Yes/No"** with every blocker named. Below it: what
the agent is doing now and when it acts next, the closest opportunities with the
rules that still fail, active trades with planned risk and stop/target distance
and protection, pending orders through the broker lifecycle, portfolio risk,
alerts that stay until acknowledged, and the decision journal (readable lines,
tap for the audit). Kill switch and Stop reach a sleeping agent within 2 seconds.

## The improvement loop

1. **Data** — sync history, look at coverage and quality flags.
2. **Backtests** — run a strategy over a range; metrics vs SPY buy-and-hold.
3. **Experiments** — grid / random / **walk-forward** parameter search with
   in-sample → out-of-sample decay and a neighbourhood stability score. The
   screen separates adaptive OOS, a fixed-candidate replay, and the final
   untouched validation window.
4. **Promote** — install the winning params on the strategy row (versioned,
   history kept). Enable it. It trades fake currency from the next bar.
5. **Journal** — the agent writes an end-of-day entry; if live expectancy
   drifts far below the backtest baseline the strategy is auto-disabled.
6. **Coach** — with `ANTHROPIC_API_KEY`, Claude reviews recent sessions and
   proposes experiments you can run with one click.
7. **Graduate** — when the checklist is green, move the strategy up a stage.
8. **Nightly auto-research** (`manage.py auto_research`, launchd 02:10): walk-forward
   every enabled strategy on trailing real bars; compare the fixed candidate
   and current champion on identical final held-out bars; promote only when the
   candidate clears every cost-adjusted gate; journal it.
9. **Evidence audit** (`manage.py audit_qualifications`, add `--apply` to
   persist): show exactly why each immutable strategy version is unproven,
   qualified, or quarantined.

## Direction is the strategy's call

Every strategy evaluates longs and shorts on every bar and picks whichever
setup is there (a breakdown shorts, a cross down shorts, a stretch above VWAP
shorts). The only veto is the venue's: crypto spot cannot be shorted. There is
no switch for the operator to set; the nightly walk-forward decides whether a
strategy's shorts earn their keep.

## Accounts & people

Sign-up (`/accounts/signup/`) is **invite-only**: emails in `SIGNUP_ALLOWED_EMAILS`
can always join as operators; everyone else needs an invite from
Settings → People and is view-only until promoted. Password reset, email
management and passkeys are django-allauth; without `EMAIL_HOST` the links
print to the server console (and show on the page in dev).

## Commands

`bootstrap_admin`, `seed_watchlist`, `sync_bars`, `sync_calendar`,
`run_agent`, `backtest`, `optimize`, `eod_journal`, `flatten_all`, `check_env`.
Every one has `--help`.

## Phone access

- Tailnet: `python3 ~/.local/share/moneytree-tls/proxy.py` terminates TLS on
  `:8446` → `https://kironkps-macbook-pro-2.taildfcf4.ts.net:8446/`
- Public quick tunnel: `~/.local/bin/cloudflared tunnel --url http://localhost:8003`
  (dev CSRF trusts `*.trycloudflare.com` and `*.ts.net`).

## v2 — real money

1. Open an Alpaca **live** brokerage account, fund it (~$10k to match the seed).
2. Put the live keys in `.env`, set `LIVE_TRADING_ARMED=1`, restart the server.
3. Settings → Arm LIVE (type `ARM LIVE`). Lower the limits. Start
   `run_agent --mode live`. Only **Tree**-stage strategies trade.
4. The PDT $25k rule was abolished on 2026-06-04; your broker's margin
   minimum and T+1 settlement still apply.

## Ports on this Mac

findit 8000/443 · inflow 8001/8444 · secretary 8443/3000 · kre8essence 8002/8445 · **moneytree 8003/8446**.
