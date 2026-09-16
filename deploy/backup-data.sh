#!/bin/bash
# Back up the trading data. Called by deploy/git-push.sh before the 02:00 push.
#
# Two destinations, both on the private GitHub repo, because nothing else on
# this machine is off-machine (no iCloud, no Dropbox, no Time Machine, no
# external drive):
#
#   1. data-backup/ledger.sql — every night, committed and versioned. The money
#      and the decisions: accounts, orders, fills, trades, positions, equity
#      snapshots, signals, cards, risk events, strategies, experiments, journal.
#      ~1.7 MB of plain SQL text, which git deltas well because a dump is
#      append-ordered. Every night of history stays recoverable from git.
#   2. A full db.sqlite3.gz (~27 MB) as a GitHub *release* asset, Sundays.
#      Release assets do not live in the git history, so the big binary never
#      bloats the repo. This is the one that also carries the bars.
#
# Deliberately NOT in the nightly dump: main_app_bar (re-downloadable with
# `sync_bars`), main_app_feedevent (narration, pruned after 24 h anyway),
# main_app_symbolstate (a scratch pad rewritten every bar), backtestrun /
# backtesttrade (regenerable by re-running the backtest; their conclusions live
# in experiment, strategy.history and journalentry, which ARE dumped). Those
# ride the weekly full snapshot instead.
#
# Pure sqlite3 and gh on purpose — no Django, no pipenv. The backup has to keep
# working on a night when the app itself is mid-edit and will not import.
set -u

REPO=${MONEYTREE_REPO:-/Users/kironkp/code/moneytree}
DB="$REPO/db.sqlite3"
OUT="$REPO/data-backup"
LOG="$REPO/run/git-push.log"
SQLITE=/usr/bin/sqlite3
GH=/usr/local/bin/gh
REMOTE_REPO=${MONEYTREE_GH_REPO:-kironkp/money-tree}
RELEASE_TAG=db-snapshot

# The irreplaceable rows, in a fixed order so the dump diffs cleanly.
# The news tables are here because they are evidence, not cache: a verdict is a
# forecast recorded before the fact, and once the day passes it cannot be
# regenerated from anything. They were missing until 2026-09-17, so the only
# copy of every judgement the News Agent had ever made lived on one laptop.
TABLES='main_app_account main_app_agentconfig main_app_agentrun main_app_apiusage
        main_app_briefing main_app_experiment main_app_fill main_app_instrument
        main_app_journalentry main_app_marketsession main_app_newsitem
        main_app_newssession main_app_newsverdict main_app_order main_app_position
        main_app_riskevent main_app_signal main_app_signupinvite main_app_strategy
        main_app_trade main_app_tradecard main_app_equitysnapshot'

say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "backup: $*" >> "$LOG"; }

full_wanted() {
    [ "${1:-}" = '--full' ] && return 0
    [ "$(date '+%u')" = '7' ] && return 0   # Sundays
    return 1
}

mkdir -p "$OUT" "$REPO/run"
[ -f "$DB" ] || { say "FAILED: no database at $DB"; exit 1; }

work=$(mktemp -d) || { say 'FAILED: no temp dir'; exit 1; }
trap 'rm -rf "$work"' EXIT
snap="$work/snapshot.sqlite3"

# VACUUM INTO takes a consistent copy without blocking the agents that are
# writing to the WAL — a plain file copy could catch a half-written page.
if ! "$SQLITE" "$DB" "VACUUM INTO '$snap'" 2>>"$LOG"; then
    say 'FAILED: could not snapshot the database'
    exit 1
fi

# --- 1. the nightly ledger dump ------------------------------------------
# The whole schema plus the ledger rows, so the file restores to a database
# the app can actually open — empty of bars, not missing their tables.
# django_migrations rides along so `migrate` on the restored file is a no-op.
{
    printf -- '-- MoneyTree ledger backup %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    printf -- '-- Restore:\n'
    printf -- '--   sqlite3 restored.sqlite3 < ledger.sql\n'
    printf -- '--   cp restored.sqlite3 db.sqlite3\n'
    printf -- '--   manage.py bootstrap_admin      # the login is not in this file\n'
    printf -- '--   manage.py sync_bars --days 60  # nor are the bars\n'
    printf -- 'PRAGMA foreign_keys=OFF;\n'
    printf 'BEGIN TRANSACTION;\n'
    # sqlite_sequence is created by SQLite itself; re-creating it is an error.
    "$SQLITE" "$snap" '.schema' | grep -v '^CREATE TABLE sqlite_sequence'
    for t in $TABLES django_migrations; do
        "$SQLITE" "$snap" -cmd ".mode insert $t" "SELECT * FROM $t;"
    done
    printf 'COMMIT;\n'
} > "$work/ledger.sql" 2>>"$LOG"

if [ ! -s "$work/ledger.sql" ]; then
    say 'FAILED: the ledger dump came out empty'
    exit 1
fi
mv "$work/ledger.sql" "$OUT/ledger.sql"

# --- 2. a human-readable manifest, so `git log` tells the story -----------
{
    printf 'MoneyTree data backup\n'
    printf 'taken       %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    printf 'database    %s bytes\n' "$(wc -c < "$DB" | tr -d ' ')"
    printf 'ledger.sql  %s bytes\n\n' "$(wc -c < "$OUT/ledger.sql" | tr -d ' ')"
    printf 'accounts (mode · market · equity · trades)\n'
    "$SQLITE" "$snap" "SELECT '  ' || a.mode || ' · ' || a.market || ' · ' ||
        printf('%.2f', a.equity) || ' · ' || (SELECT COUNT(*) FROM main_app_trade t WHERE t.account_id = a.id)
        FROM main_app_account a ORDER BY a.market, a.mode;"
    printf '\nrows backed up\n'
    for t in $TABLES; do
        printf '  %-28s %s\n' "$t" "$("$SQLITE" "$snap" "SELECT COUNT(*) FROM $t;")"
    done
    printf '\nnot in this dump (regenerable)\n'
    for t in main_app_bar main_app_feedevent main_app_symbolstate main_app_backtestrun main_app_backtesttrade; do
        printf '  %-28s %s\n' "$t" "$("$SQLITE" "$snap" "SELECT COUNT(*) FROM $t;")"
    done
} > "$OUT/MANIFEST.txt" 2>>"$LOG"

say "ledger.sql $(wc -c < "$OUT/ledger.sql" | tr -d ' ') bytes, $("$SQLITE" "$snap" 'SELECT COUNT(*) FROM main_app_trade;') trades"

# --- 3. the weekly full snapshot, as a release asset ----------------------
if full_wanted "${1:-}"; then
    if [ ! -x "$GH" ]; then
        say 'warning: gh not found — skipped the full-database upload'
        exit 0
    fi
    gzfile="$work/moneytree-db-latest.sqlite3.gz"
    gzip -9 -c "$snap" > "$gzfile" || { say 'warning: could not compress the snapshot'; exit 0; }
    size=$(wc -c < "$gzfile" | tr -d ' ')
    "$GH" release view "$RELEASE_TAG" --repo "$REMOTE_REPO" >/dev/null 2>&1 || \
        "$GH" release create "$RELEASE_TAG" --repo "$REMOTE_REPO" --title 'Database snapshot' \
              --notes 'Full db.sqlite3 (gzipped), replaced weekly by deploy/backup-data.sh. Includes the bars and the feed; the per-night ledger history lives in data-backup/ledger.sql.' \
              >/dev/null 2>>"$LOG"
    if out=$("$GH" release upload "$RELEASE_TAG" "$gzfile" --clobber --repo "$REMOTE_REPO" 2>&1); then
        say "uploaded the full database to the $RELEASE_TAG release ($size bytes)"
    else
        say "warning: full-database upload failed — $(printf '%s' "$out" | tr '\n' ' ' | tail -c 160)"
    fi
fi

exit 0
