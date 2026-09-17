# MoneyTree

Django 6.1 day-trading agent. v1 trades fake currency ($10,000 seed); v2 flips
the same code to real money. Findit/kre8essence conventions: Pipenv, single
`main_app` + `main_app/services/`, vanilla CSS tokens (dark default), HTMX 1.9
CDN, SQLite dev / Postgres on Heroku via `ON_HEROKU`, `VERSION` in settings.

## Layout

- `main_app/services/` holds all logic; views stay thin (`main_app/views/`).
  - `data/` — providers (`alpaca_data`, `yahoo`, `synthetic`), `store` (upsert,
    load, gap-fill sync, **quality gate**), `calendar` (pure NYSE sessions
    2024–27, `MarketSession` overrides).
  - `strategies/` — `base.Strategy` (`params` schema, causal `prepare()`,
    `on_bar()` → `Signal`s), `orb`, `vwap_reversion`, `ema_momentum`, `registry`.
  - `broker/` — `base` (float dataclasses, `evaluate_exit`), `sim` (fills:
    next-bar-open or immediate ± slippage, stop gap-through, stop-before-
    target, limit targets, fees per asset class, liquidity cap, whole shares),
    `alpaca` (paper/live; brackets for stocks, engine-managed exits for crypto;
    startup `sync()` reconciles by client_order_id; live needs
    `LIVE_TRADING_ARMED=1` **and** `AgentConfig.mode == live`).
  - `risk.py` (sizing = risk$/stop distance, caps, daily loss halt, session
    offsets), `engine.py` (**one `process_bar` for backtest, replay and
    live**; `run_frames` drives history with optional `act_from` warm-up),
    `backtest.py` (pure `run_backtest` + Django persistence), `metrics.py`,
    `optimize.py` (grid/random/walk-forward, fork pool of 2, stability),
    `ledger.py` (DB recorder, persist/hydrate the simulator), `agent.py`
    (live loop, replay, catch-up, lock, SIGTERM flatten), `journal.py`,
    `coach.py` (Claude, dormant without key), `promotion.py` (graduation
    checklist), `control.py` (flatten from the web), `procs.py` (subprocesses).
- Backtests run inline in the request; experiments, replays, syncs and the
  agent are `manage.py` subprocesses (`procs.spawn_manage`) logging to `run/`.

## v1.1 additions

- **Markets**: `Account`, `Strategy` and `AgentRun` carry `market` (stocks|crypto).
  One agent process per account (`run_agent --market`), lock `run/agent-{mode}-{market}.lock`,
  log `run/agent-{mode}-{market}.log`. Strategy URLs are `/strategies/<market>/<key>/`.
  `market_for_symbols()` picks the promote target for a backtest/experiment.
- **Live feed**: `FeedEvent` rows via `services/narrator.py` (buffered, flushed per
  tick; backtests get no narrator). Engine narrates fills/trades/signals/blocks/time
  exits; strategies implement `explain()` for the per-bar "thoughts" line. Poller:
  `static/js/feed.js` → `/api/feed/?account=&market=&after=`.
- **Auth**: django-allauth (email login, invite-only signup via `InviteSignupForm`
  + `SignupInvite`, password reset, passkeys). `operator_required` /
  `deny_observer` gate every mutating view; `is_staff` = operator. Owner rows are
  verified by `bootstrap_admin`.
- **Data hygiene**: live agents load frames with `exclude_sources=['synthetic']`;
  the Data page offers a one-click replacement with real bars.
- **Tabs**: `static/js/tabs.js` traveling indicator + `@view-transition` for
  cross-page flow (Secretary's nav-tabs feel).

## v1.2 — the "game time" slice (execution truth before appearance)

- **Trade cards** (`TradeCard`, engine `CardState`): one record per trade with the
  correlation id = entry client_order_id; states awaiting_approval → approved →
  submitted → accepted → partially_filled → filled → protected → closing → closed
  (or rejected/canceled/expired). `hydrate_cards()` restores open cards on restart.
- **Symbol states** (`SymbolState`): every rule with value/threshold/pass-fail from
  `Strategy.rules()`, plus the plain-English summary — feeds "closest opportunities".
- **Feed phases**: observe/evaluate/decide/size/submit/fill/manage/close/alert;
  `ts` = wall clock, `bar_ts` = the market bar. `feed.js` renders summaries with an
  expandable audit and shows DISCONNECTED on failed polls.
- **One coordinated close**: `SimBroker` marks `closing`, rejects duplicate exits,
  cancels in-flight exits before an immediate close, never fills an exit without a
  position (no reversals). `AlpacaBroker.close_position` cancels protection →
  confirms → closes the venue's remaining qty; crypto gets a venue-side stop-limit
  after the entry fill (`_place_protection`).
- **Reconciliation**: `AlpacaBroker.sync()` every tick reports adopted/closed/
  diverged; divergence blocks entries (`RiskManager.blocks['reconcile']`) until clean.
  `Account.last_reconcile_at/reconcile_ok/reconcile_note` drive the status strip.
- **Kill means now**: the agent polls `AgentConfig`/`AgentRun.stop_requested`/
  approvals every 2 s inside `_sleep`; risk settings hot-reload between bars;
  strategy edits raise a `config_changed` alert (restart to apply).
- **Risk survives restarts**: `Account.day_entries/day_halted/day_halted_reason`
  restored via `RiskManager.restore()`; `AgentRun.expected_interval_s/state/
  next_action_at/last_bar_ts` feed `AgentRun.health` (healthy/waiting/stale/disconnected).
- **Strategies own positions**; `allocation_pct` caps exposure; graduation is
  enforced (override needs a reason); `live_confirm_orders` = operator approval cards.
- **Watchdog** (`manage.py watchdog`, launchd every 5 min) closes paper/live positions
  whose agent is dead or stale. `services/status.py` builds "Safe to trade now" with
  explicit blockers.

## v1.3 — Degen lane, pulse, auto-research

- `Market.DEGEN`: altcoins (`Instrument.market='degen'`), 1Min bars, `AgentConfig.degen_*`
  risk overrides via `RiskConfig.from_model(cfg, market)`; `burst` strategy
  (`strategies/burst.py`). Prices are 8-decimal Decimals now (PEPE/SHIB/BONK).
- Pulse: `Agent.pulse()` every `pulse_seconds` inside `_sleep` — Alpaca latest QUOTE
  mid (trades are too sparse on Alpaca's crypto venue); marks positions, updates
  `SymbolState.price`, writes `level='pulse'` feed lines (pruned after 24 h).
- `manage.py auto_research` + `com.kiron.moneytree.research.plist` (02:10 nightly).

## v1.4 — symmetric shorts, per-feed histories

- No `allow_short` / `trade_short` any more: strategies emit long or short signals
  symmetrically; `RiskManager` only vetoes crypto-spot shorts. Simulator shorts
  use cash-account semantics (no borrow fees modelled yet — TODO before live).
- `Bar` is unique per (instrument, timeframe, **source**, ts). The live loop reads
  and writes the feed it trades on (`alpaca:iex` for stocks; `sync_bars --provider
  alpaca-iex` keeps 60 days of it) so relative volume compares like with like;
  backtests/replays default to the best source (`store.best_source`, SIP first).
  Never `load_frame` without a source and expect one feed — the default picker
  handles it.

## v1.5 — Forex lane

- `Market.FOREX` / `AssetClass.FOREX`: four USD-quoted majors (EUR, GBP, AUD, NZD
  against USD — every P&L is already in dollars; USD/JPY-style pairs need a quote
  conversion, not built). Data: **Yahoo only** (`EURUSD=X`, 59 days of intraday
  bars, no centralized volume → relative volume is explicitly unavailable;
  EMA uses a documented price-only fallback and VWAP reversion labels its
  equal-weighted session-mean fallback instead of inventing volume).
  `sync_bars` swaps to Yahoo for forex instruments whatever `--provider` says.
- Hours: 24/5, Sunday 17:00 → Friday 17:00 ET (`calendar.forex_is_open`,
  `forex_next_open`, `forex_week_close`). The forex *day* rolls at 17:00 ET
  (`calendar.trading_day(ts, 'forex')`, `indicators.session_key`); the *week* is the
  session: `minutes_to_close` counts down to Friday 17:00 ET, so the engine's usual
  "no entries in the last 30 min / flat 5 min before the close" rules close the week,
  and the agent flattens any leftover the moment the lane closes (`'weekend'`).
- Margin: `RiskConfig.leverage` (forex 10×, everything else 1×) → `SimBroker`
  buying power = equity × leverage − gross exposure − pending entries. Cash goes
  negative on a forex entry, equity does not. `forex_max_position_pct` 500 caps one
  position at 5× equity. Cost model = spread: `fee_bps_forex` 0.5 + `forex_slippage_bps`
  0.3 per side (1.6 bps a round trip), cost gate 2×. `forex_max_hold_minutes` 240.
- Lane hours are now generic: `Account.lane_asset_class` / `is_open_at(now)`,
  `cal.is_open(ts, asset_class)`, `cal.next_open(ts, asset_class)`. The agent loop
  runs on `lane_open` + `_daily_roll`: stocks flatten at the bell and journal;
  round-the-clock lanes journal yesterday at midnight ET and **keep positions**
  (the old loop flattened crypto at 16:00 ET by accident).
- `spec_from_models` now builds `RiskConfig.from_model(cfg, market_for_symbols(symbols))`
  so backtests/walk-forwards price each lane's own costs and leverage.
  `market_for_symbols` uses a set — `.distinct()` on a model with `Meta.ordering`
  silently added `symbol` to the DISTINCT and returned one row per symbol.
- Forex has no broker adapter: `run_agent --market forex` refuses paper/live
  (OANDA v20 practice is the plan; needs an account). Evidence 2026-09-06 (59 days):
  EMA and VWAP both lose about their costs on every timeframe (5Min PF 0.42,
  15Min 0.83, 1Hour 0.87 over 2 years) — the lane runs at 15Min so the spread does
  not shred it. This historical v1.29 observation policy was superseded in
  v1.32: unvalidated defaults now remain disabled at Seed.

## v1.30 — Evidence before execution

- Walk-forward now separates three different claims: adaptive-policy OOS
  (one preceding-training winner per window), a fixed-candidate historical
  replay, and the final untouched test window. Nightly research reruns the
  current champion on that exact final window. Only the identical-window
  candidate/champion result can confirm or promote a configuration; PF ≥ 1.10,
  positive net and expectancy, and the experiment's minimum trades are hard
  gates. Manual walk-forward promotion uses the same gate and never attaches
  adaptive metrics to a fixed config.
- `Strategy.qualification` is independent from `enabled` and `stage`:
  `unproven|qualified|quarantine`. Sim/replay may observe unproven versions;
  paper/live agents query only qualified versions. Thirty forward trades with
  PF < 1, non-positive expectancy, or non-positive net triggers sticky
  quarantine and disables the strategy. New/manual params reset to unproven.
  `audit_qualifications [--market ...] [--apply]` explains or persists it.
- The status strip reports **Operational**, **Evidence**, and **Execution**
  separately. A healthy feed is not a profitable strategy and a simulator may
  be authorized to collect data while evidence remains unproven. Broker-stage
  qualification cannot be overridden.
- Missing/zero volume remains NaN. Volume-filtered crypto/stock entries block
  when the requirement cannot be measured; a zero threshold is explicitly
  shown as disabled. Spot FX states that centralized volume is unavailable and
  uses its documented price-only fallback. Session VWAP labels its equal-
  weighted fallback.
- Tests force non-manifest static storage regardless of `.env`, so the normal
  `manage.py test` command is deterministic. Migration `0009` adds the
  qualification ledger. `docs/LOGBOOK.md` is the release evidence record.

## v1.32 — Stop operational losses and concentrated FX bets

- Default strategies now start disabled at Seed. Migration `0010` parks only
  untouched Forex v1 defaults (unproven, enabled, and with no history); it
  never overrides a user-promoted version.
- Forex retains 10× total margin but caps each USD direction at 5× equity.
  Pending/approval-stage entries reserve position slots, buying power,
  strategy allocation, and directional capacity. Entries below 10% of their
  planned size are rejected instead of creating dust trades.
- Dashboard Stop remains an intentional flatten. A launchd/deploy signal now
  preserves sim/paper positions for restart, preventing infrastructure from
  creating manual exits and extra costs. Live still follows
  `LIVE_FLATTEN_ON_EXIT`.
- Portfolio risk uses each lane's actual daily-loss budget, shows the dominant
  directional exposure, and Settings includes Forex accounts.
- GitHub Actions runs the complete Django suite on pushes and pull requests.

## v1.31 — No lucky-window promotions

- A walk-forward training winner must itself have enough trades, PF above
  1.0, positive net P&L, and positive expectancy after costs. If the latest
  chronological training window has no viable winner, the experiment emits
  no recommendation; it never falls back to a stale or least-bad config.
- Walk-forward promotion now requires two independent gates: the final fixed
  candidate holdout (10+ trades, PF ≥ 1.10, positive net and expectancy) and
  the adaptive selection pipeline (30+ trades with the same return gates).
  Automation and the web promotion endpoint enforce the same contract, and
  the experiment screen shows each gate separately.
- This was driven by Forex experiment #27: a lucky final week suggested VWAP
  v2 at PF 1.102 and +$76.29, even though four of five adaptive OOS windows
  lost and the pipeline totaled PF 0.51 / −$1,961.62. Corrected experiments
  #28–29 returned no valid Forex candidate. Nothing was promoted.

## v1.33–1.36 — the News Agent learns to research

One version per build, two decimals, tagged: **1.33** Phase 0, **1.34** Phase 1,
**1.35** Phase 2, **1.36** Phase 3.

- **v1.33 — measurement before intelligence.** Price and ATR are stamped on every
  verdict, not only the ones that traded (6 of 266 → 182), so the scoreboard has a
  control group. Outcomes replay the barrier race the trade implied — 1.5 ATR stop,
  3 ATR target, the lane's own `max_hold` — anchored at the first bar the lane was
  actually open and sized from the ATR of the entry bar, recording
  `outcome_kind`/`outcome_atr_net`/MFE/MAE. Rebuilt rows are `provenance =
  reconstructed` and excluded from every statistic. Instructions use an atomic
  lease (`available → leased → consumed`) keyed on the EVENT, with a DB uniqueness
  constraint — the four QQQ shorts from four sittings were that race. `news_risk.py`
  holds the arm's own preregistered limits. `store.covering_frame` picks the feed
  that covers the window: `best_source` ranks by overall quality, so every live
  lookup had been returning bars from 1 September. Ledger: one price table with
  Flex/Standard split, reservations that fail loudly, the web-search fee recorded.
- **v1.34 — provenance.** `first_public_at` / `ingested_at` / `source_updated_at`
  on every headline; freshness measured from first publication only. A wire story
  revised three times is one event with a revision counter, not three. Article
  bodies via `include_content=True` (2,481 chars vs 144), with `<script>`/`<style>`
  contents dropped, not merely untagged. `services/research/` grades sources in
  three tiers: **filed** (SEC EDGAR, with accession/form/period/XBRL tag),
  **vendor** (yfinance, never authoritative), **measured** (our bars). Every field
  nullable; gaps recorded as gaps.
- **v1.35 — dossiers in shadow.** `services/dossier.py` researches one company at a
  time on `gpt-5.6-terra`/Flex via the Responses API. Numbers are supplied, not
  recalled; anything the model introduces needs a verbatim quote and a URL or it is
  dropped. It forecasts the barrier race (must sum to 1), never a magnitude.
  `barrier_base_rate` supplies the measured prior. CATALYST must be dated and under
  24h or it is demoted to context; size multiplier is clamped to ≤ 1.0. Measured
  $0.0483 per dossier.
- **v1.36 — the preregistered gate.** `preregistration.py` hashes the model, prompt,
  schema and every limit into a fingerprint; changing any of it supersedes the
  evaluation and restarts at n=0. `evaluation.py` decides ONLY at preregistered
  checkpoints (20/40/60/90/120 days) with O'Brien-Fleming alpha spending, on a
  circular block bootstrap that survives serial dependence, with power in the
  sample-size calculation and Holm across the two confirmatory claims. Promotion
  opens a fresh decay epoch at n=0. `ACT_SOURCES = ('headline',)` makes shadow a
  property of the query. A regression test runs 40 null histories and requires that
  noise is not promoted.

## v1.38 — The Floor (`/map/`)

An interactive map of every autonomous part of the desk: **52 nodes, 81 cables**.
Nodes are draggable; cables sag under their own weight and swing when you move
what they are plugged into. Click anything for what it is, what it costs, what
feeds it and what it feeds.

- `services/graph_model.py` is the source of truth — every node's plain-English
  description is written by hand, because the point of the map is to say what a
  part is FOR and no amount of introspection produces that. `services/graph_state.py`
  merges live values in at request time (24 queries, ~36 ms). Two measured rules:
  never touch `Bar` from this endpoint (200 ms+), never call `build_status()` per lane.
- **Hue is lane, form is payload, and lane colour never touches type.** Lane colour
  lives only in chrome — a node's spine, a cable's stroke. Green and red keep
  typography to themselves because they already mean money won and money lost.
  The seven node kinds have genuinely different silhouettes so the machine reads
  at a zoom where no text does.
- **Cable shape is a closed form, not a solved catenary**: `s = L·√k·(0.6124 −
  0.1124k)` where `k = 1 − d/L`. Within **1.26%** of a Newton-solved catenary
  across 2%–1900% slack, exact at both physical limits (folded → L/2, taut → 0),
  one `sqrt`, no transcendentals. The cubic's control points drop by **4s/3**;
  2s/3 is the classic wrong answer that never hangs enough.
- **Physics is verlet with projected distance constraints**, not springs: a cable
  is inextensible, so the stiffness a spring needs is the stiffness that makes
  explicit integration explode. Seven points, three relaxation passes, fixed
  1/60 s step with a bounded accumulator. Slack `L` is recomputed only on arrange,
  so during a drag only the chord changes — which is what makes a cable go taut
  when pulled and pile up slack when pushed, with no extra state.
- **Everything sleeps.** A settled cable is removed from the simulation and the
  RAF loop stops entirely; `awake` reaching zero ends the frame loop.
- Nodes are divs moved only by `transform`; cables are paths in one world-
  coordinate SVG; pan/zoom is a single transform on a shared wrapper. No layout
  reads inside the frame loop.
- Nine **equation nodes** render the real mathematics in HTML and ~45 lines of CSS
  — no KaTeX, no MathJax, no build step. Variables are serif italic, literal
  numbers are mono, so a substituted formula is scannable.
- Fonts: **Archivo** (display, expanded) and **Source Serif 4** (maths) added to
  the existing Inter + IBM Plex Mono link — one extra family on the same URL, no
  new origin. `graph.css` is a second stylesheet loaded only on this route.
- The map shows what is **actually loaded**, not what is written down: the
  watchdog's plist exists and has never been loaded on this machine, so nothing
  would close a position if a lane agent died holding one. That is drawn in red.

## v1.39 — what is scheduled vs what is running

Nine launchd jobs exist; only seven had ever been loaded. The two that had not
were the two that keep the desk alive.

- **The watchdog was never loaded.** Its plist sat in `deploy/launchd/` for weeks
  looking exactly like a job that was working. If a broker-backed agent had died
  holding a position, nothing would have closed it. Now loaded and verified.
- **Nothing started the agents after a reboot.** The four lanes were launched only
  by `restart_agents` inside the 02:10 nightly research job, so a reboot at 10am
  meant no trading until 02:10 the following night, silently. New
  `manage.py ensure_agents` is idempotent — it starts only what is missing — so
  `com.kiron.moneytree.agents.plist` can carry `RunAtLoad` plus a 5-minute
  interval. `restart_agents` stays for the post-promotion reload, where killing
  and respawning is the point.
- The old `com.kiron.moneytree.agent.plist` is **deleted**. It predated the
  four-lane split: `run_agent --mode sim` with no `--market` would have started a
  second stocks agent contending for the first one's lock.
- **The map now reads job status from the job's log, not from its plist.** A
  schedule file says what someone intended; a log written five minutes ago says
  what is running. `graph_state._job()` stats the log and reports live, stale or
  never-run. This is the whole reason the gap went unnoticed for weeks.

## v1.40 — a free permanent public URL, and DEBUG off

**Public link: `https://kironkps-macbook-pro-2.taildfcf4.ts.net:10000/`** — a
Tailscale Funnel. Free on the personal plan, permanent, a real Let's Encrypt
certificate, reachable from any device without Tailscale installed. No dyno, no
bill. `tailscale funnel --bg --https=10000 8003`; the config persists across
reboots.

Port 10000 because Funnel only offers 443, 8443 and 10000: 443 is FindIt's on
this machine and 8443 is Secretary's. Note the consequence found the hard way —
from *inside* the tailnet the hostname resolves to the machine itself, so a
funnel on 443 was silently served by FindIt instead.

- **Heroku is scaled to zero.** It had been deployed with no config vars and no
  add-ons at all, so it fell to every development default: `DEBUG=True` and the
  repo's placeholder `SECRET_KEY`, publicly, plus SQLite on an ephemeral
  filesystem — the release-phase `migrate` ran into a container that was then
  discarded, which is why `socialaccount_socialapp` did not exist. Everything
  keys off `ON_HEROKU`, which was never set.
- **New funnel mode in settings.** `FUNNEL_HOST` + `FUNNEL_PORT` produce
  `FUNNEL_ORIGIN`, which feeds `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.
  Setting `FUNNEL_HOST` is what makes a laptop a deployment: `DEBUG=False`,
  manifest static storage, `SECURE_PROXY_SSL_HEADER` (the funnel terminates TLS
  and forwards plain HTTP), and Secure cookies — the last scoped to funnel mode
  so `http://127.0.0.1:8003` still works locally.
- **CSRF failures are now logged.** Django explains a rejection only when DEBUG
  is on, which is exactly when it is least needed; over a public funnel a 403 was
  a silent wall. `django.security.csrf` at WARNING says which check failed, and
  said so within a minute of being added.
- `collectstatic` is required now that DEBUG is off. `WHITENOISE_MANIFEST_STRICT`
  is False, so a missing asset degrades rather than 500s.

## Invariants that matter

- Bars are stamped at bar START (Alpaca, Yahoo, synthetic alike). The live
  loop only acts on bars whose end + grace has passed
  (`store.complete_bars_only`), and never twice on one bar (`Engine.last_acted`).
- History: Alpaca `feed=sip`, `end ≤ now − 15 min`, `adjustment=split`. Live
  polling: `feed=iex` explicitly. Default timeframe 5Min.
- Ledger rows are Decimal; `Bar` is float. Never do money math in templates.
- `client_order_id` is deterministic (`mt-{mode}-{strategy}-{symbol}-{bar_ts}-{leg}`).
- Session times come from offsets before the close (early closes, DST safe).
- Crypto: no brackets, no shorts, 25 bps taker fees, sessions at 00:00 UTC,
  positions age out via `max_hold_minutes`. Forex: sessions roll 17:00 ET, the
  week closes Friday 17:00 ET, leverage 10×, whole units, Yahoo bars only.
- `enabled` is permission to observe at the configured stage, not proof.
  Newly seeded rows are disabled until research is deliberately promoted.
  Broker-backed modes must also see `qualification='qualified'`; quarantine is
  sticky until a new version or an explicit reset to unproven.
- SQLite runs WAL + IMMEDIATE + 30 s timeout: web, agent and optimizer all
  write it. Don't add a fourth chatty writer.
- Template comments: `{# #}` is single-line only; multi-line → `{% comment %}`
  (a test guards this).

## Backups / remote

- **GitHub**: `origin` = `git@github.com:kironkp/money-tree` over HTTPS
  (`https://github.com/kironkp/money-tree.git`), **private**. Pushed 2026-09-07
  with all v1.x tags. Credentials come from the osxkeychain helper (gh is logged
  in as kironkp), which works from launchd without prompting.
- **Nightly push 02:00 PT**: `deploy/git-push.sh` +
  `com.kiron.moneytree.gitpush.plist` (loaded 2026-09-07). It commits whatever
  is uncommitted as "Nightly snapshot <date>", pushes commits, then pushes tags
  separately (the release ritual makes *lightweight* tags, which
  `--follow-tags` skips). Locks against overlap in `run/git-push.lock`, clears a
  stale lock after an hour, `GIT_TERMINAL_PROMPT=0` so a credential problem is a
  log line and not a hung process. Log: `run/git-push.log`.
- If `main` has diverged (another agent or machine pushed first) it never
  rebases and never forces: it parks the local work on `backup/<date>-<sha>` and
  says so in the log, leaving `main` for a human. **Codex also works in this
  repo** — expect its in-progress edits to land in the nightly snapshot commit.
- **Trading data** (`deploy/backup-data.sh`, run by the push script before it
  commits). Two destinations, because nothing else here is off-machine — no
  iCloud, no Dropbox, no Time Machine, no external drive:
  - `data-backup/ledger.sql` — **nightly, committed, versioned**. Full schema +
    the irreplaceable rows (accounts, orders, fills, trades, positions, equity
    snapshots, signals, cards, risk events, strategies, experiments, journal,
    agent runs) + `django_migrations`. ~1.7 MB of plain SQL, which git deltas
    well because a dump is append-ordered — never gzip it, that would defeat
    the packfile. `data-backup/MANIFEST.txt` beside it carries row counts and
    per-account equity, so `git log -p data-backup/MANIFEST.txt` reads as an
    account history. **Restore is verified**: `sqlite3 new.db < ledger.sql`
    gives a database Django opens with no pending migrations.
  - The **full `db.sqlite3.gz`** (~29 MB) as a GitHub **release** asset on tag
    `db-snapshot`, refreshed Sundays (`date +%u` = 7) or with `--full`. Release
    assets live outside the git history, so the big binary never bloats the
    repo; `--clobber` keeps exactly one, the latest.
  - Left out of the nightly dump on purpose: `main_app_bar` (re-downloadable
    with `sync_bars`), `main_app_feedevent` (narration, 74 MB, pruned at 24 h),
    `symbolstate` (rewritten every bar), `backtestrun`/`backtesttrade`
    (regenerable; their conclusions are in experiment/strategy.history/journal).
    All of those ride the weekly full snapshot.
  - No `auth_user`: password hashes stay out of git. `bootstrap_admin` recreates
    the login on a restore.
  - Pure `sqlite3` + `gh`, no Django and no pipenv, so the backup still runs on
    a night when the app is mid-edit and will not import. `VACUUM INTO` takes a
    consistent copy while the agents keep writing.
- Only code goes up otherwise: `db.sqlite3`, `backups/` (the v1.x snapshots are
  ~200 MB, over GitHub's 100 MB file limit), `run/` and `.env` are gitignored.
- `MONEYTREE_REPO` overrides the script's target repo — that is how it gets
  tested against a scratch clone without touching the real history.

## Local dev

- Port **8003** (map: findit 8000/443, inflow 8001/8444, secretary 8443/3000,
  kre8essence 8002/8445, moneytree 8003/8446). TLS proxy for the tailnet:
  `python3 ~/.local/share/moneytree-tls/proxy.py` (cert copied from
  findit-tls, expires 2026-11-30 — re-issue with `Tailscale cert`).
- `pipenv run python manage.py test` — one TestCase class per behaviour.
- Owner login is `kiron` / `DJANGO_SUPERUSER_PASSWORD` in `.env`.
- Release ritual: bump `VERSION`, `git tag vX.Y`, snapshot
  `backups/db-vX.Y.sqlite3`.

## Library gotchas

pandas 3: Copy-on-Write (use `.loc`), default `str` dtype, `datetime64[us]`,
`ffill()` not `fillna(method=)`. Django 6.1: no `timezone.utc` (use
`datetime.UTC`). yfinance: `prepost=False`, `auto_adjust=False`, drop the
in-progress last row, back off on 429. alpaca-py 0.44: `TradingClient(paper=)`,
`StockBarsRequest(feed=DataFeed.IEX|SIP, adjustment=Adjustment.SPLIT)`,
crypto symbols `BTC/USD`, `GetOrdersRequest(status=QueryOrderStatus.CLOSED)`.
The Alpaca broker adapter has not yet been exercised against a real account
(no keys at build time) — expect small mapping fixes on first paper run.
