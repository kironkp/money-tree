#!/bin/bash
# Nightly backup push (launchd: com.kiron.moneytree.gitpush, 02:00 PT).
#
# Commits whatever is uncommitted, then pushes commits and tags to GitHub.
# Only the code goes up: db.sqlite3, backups/, run/ and .env are gitignored,
# so the trading ledger is NOT backed up here (the snapshots are far over
# GitHub's 100 MB file limit).
#
# Never prompts: GIT_TERMINAL_PROMPT=0 makes a missing credential an error in
# the log instead of a process that hangs until the next run.
set -u

REPO=${MONEYTREE_REPO:-/Users/kironkp/code/moneytree}   # overridable so the script can be tested against a scratch repo
GIT=/usr/bin/git
LOG="$REPO/run/git-push.log"
LOCK="$REPO/run/git-push.lock"
export GIT_TERMINAL_PROMPT=0

mkdir -p "$REPO/run"
say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }
fail() { say "FAILED: $*"; rmdir "$LOCK" 2>/dev/null; exit 1; }

cd "$REPO" || { printf 'no repo at %s\n' "$REPO" >&2; exit 1; }

# One push at a time; a stale lock older than an hour is a crashed run.
if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
        say 'clearing a stale lock (> 60 min old)'
        rmdir "$LOCK" 2>/dev/null
        mkdir "$LOCK" 2>/dev/null || fail 'could not take the lock'
    else
        say 'another push is running — skipping'
        exit 0
    fi
fi

branch=$("$GIT" rev-parse --abbrev-ref HEAD)
[ "$branch" = 'main' ] || say "note: on branch $branch, not main"

# Refresh the trading-data backup first so tonight's commit carries it. A
# backup failure must not stop the code push, so it is logged, not fatal.
if [ -x "$REPO/deploy/backup-data.sh" ]; then
    MONEYTREE_REPO="$REPO" /bin/bash "$REPO/deploy/backup-data.sh" || say 'warning: data backup failed (code push continues)'
fi

if [ -n "$("$GIT" status --porcelain)" ]; then
    files=$("$GIT" status --porcelain | wc -l | tr -d ' ')
    "$GIT" add -A || fail 'git add'
    "$GIT" commit -q -m "Nightly snapshot $(date '+%Y-%m-%d')

Uncommitted work picked up by the 02:00 backup push.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" || fail 'git commit'
    say "committed $files changed file(s): $("$GIT" log -1 --format=%h)"
fi

ahead=$("$GIT" rev-list --count '@{u}..HEAD' 2>/dev/null || echo unknown)
if out=$("$GIT" push origin "$branch" 2>&1); then
    if printf '%s' "$out" | grep -q 'Everything up-to-date'; then
        say 'nothing new to push'
    else
        say "pushed $ahead commit(s) to origin/$branch: $(printf '%s' "$out" | tr '\n' ' ' | tail -c 200)"
    fi
else
    # Someone else (another agent, another machine) pushed to main first. Never
    # rebase unattended and never force: park the work on a dated branch so it
    # is safe on GitHub, and leave main for a human to reconcile.
    say "main has diverged — $(printf '%s' "$out" | tr '\n' ' ' | tail -c 160)"
    parked="backup/$(date '+%Y-%m-%d')-$("$GIT" rev-parse --short HEAD)"
    if out=$("$GIT" push origin "HEAD:refs/heads/$parked" 2>&1); then
        say "parked this machine's work on origin/$parked — reconcile main by hand"
    else
        fail "could not park the work either — $(printf '%s' "$out" | tr '\n' ' ' | tail -c 160)"
    fi
fi

# Release tags separately: the ritual makes lightweight tags, which
# --follow-tags skips. A tag failure is worth logging, not worth failing on.
tags=$("$GIT" push --tags origin 2>&1)
if [ $? -ne 0 ]; then
    say "warning: tag push failed — $(printf '%s' "$tags" | tr '\n' ' ' | tail -c 200)"
elif ! printf '%s' "$tags" | grep -q 'Everything up-to-date'; then
    say "pushed tag(s): $(printf '%s' "$tags" | tr '\n' ' ' | tail -c 200)"
fi

rmdir "$LOCK" 2>/dev/null
exit 0
