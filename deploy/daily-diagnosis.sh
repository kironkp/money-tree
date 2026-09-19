#!/bin/bash
# 06:30 daily: run the seven-agent morning check over the live desk.
#
# Why 06:30 and not the afternoon: yesterday's numbers are settled (a mid-session
# reading once showed +$35.29 on a day that finished -$5.68, because positions
# were still open), last night's research at 02:10 and the backup at 02:00 have
# both finished, and it is still three hours before the US open — so anything it
# finds can be acted on the same day. The 17:30 report is the evening counterpart:
# morning diagnosis, evening summary.
#
# This calls Claude Code headlessly. It is the only job here that does, and it is
# the expensive one — seven agents reading the database. It is deliberately
# READ-ONLY: it diagnoses and writes a journal entry, it never changes code,
# restarts an agent or touches a position. Acting on what it finds is a person's
# job.
set -u
cd /Users/kironkp/code/moneytree || exit 1

PIPENV=/Users/kironkp/.local/bin/pipenv
CLAUDE=/Users/kironkp/.nvm/versions/node/v24.18.0/bin/claude
OUT=run/diagnosis-$(date +%Y-%m-%d).json
LOG=run/diagnosis.log

say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "diagnosis: $*" >> "$LOG"; }

# One at a time. Seven agents is a lot of machine, and two overlapping runs would
# both be slow and would race on the same journal row.
LOCK=run/diagnosis.lock
if [ -e "$LOCK" ]; then
    if [ -n "$(find "$LOCK" -mmin +180 2>/dev/null)" ]; then
        say "clearing a stale lock (older than 3h)"
        rm -f "$LOCK"
    else
        say "another run is in progress; skipping"
        exit 0
    fi
fi
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT

say "starting"
PROMPT='Run the daily-diagnosis workflow. It takes several minutes and runs seven agents.

When it finishes, write ONLY the raw JSON object it returned to the file '"$OUT"' — no prose
around it, no markdown fence, nothing else in the file. Then stop. Do not fix anything you find,
do not restart anything, do not edit any file other than that JSON. Reporting is the whole job.'

if ! "$CLAUDE" -p "$PROMPT" >> "$LOG" 2>&1; then
    say "claude exited non-zero"
fi

if [ -s "$OUT" ]; then
    if "$PIPENV" run python manage.py record_diagnosis --file "$OUT" >> "$LOG" 2>&1; then
        say "recorded $OUT"
    else
        say "could not record $OUT — the JSON is probably malformed; it is kept for inspection"
    fi
else
    say "no output file was written; nothing recorded"
fi

# Keep a fortnight. These are small and the history is the point: a finding that
# appears three mornings running is a different thing from one that appears once.
find run -name 'diagnosis-*.json' -mtime +14 -delete 2>/dev/null
say "done"
