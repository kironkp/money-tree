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
  positions age out via `max_hold_minutes`.
- SQLite runs WAL + IMMEDIATE + 30 s timeout: web, agent and optimizer all
  write it. Don't add a fourth chatty writer.
- Template comments: `{# #}` is single-line only; multi-line → `{% comment %}`
  (a test guards this).

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
