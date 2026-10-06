#!/bin/bash
# 06:30 daily: one read-only Sonnet agent runs the morning check over the live desk.
#
# Why 06:30 and not the afternoon: yesterday's numbers are settled (a mid-session
# reading once showed +$35.29 on a day that finished -$5.68, because positions
# were still open), last night's research at 02:10 and the backup at 02:00 have
# both finished, and it is still three hours before the US open — so anything it
# finds can be acted on the same day. The 17:30 report is the evening counterpart:
# morning diagnosis, evening summary.
#
# Why one agent and not seven: the seven-agent Opus workflow cost $27–60 a run,
# and from 2026-09-28 every run hit Claude Code's 600 s background-task ceiling in
# -p mode, was killed, and wrote nothing — about $410 over 14 days for no output.
# This is one synchronous `claude -p` on Sonnet with a hard dollar cap and no
# background tasks, so there is nothing for that ceiling to kill. The seven angles
# are sections of deploy/daily-diagnosis-prompt.md. The workflow
# (.claude/workflows/daily-diagnosis.js) stays for on-demand deep runs.
#
# READ-ONLY by construction, not by request: the agent's only tools are
# Read/Grep/Glob and `sqlite3 -readonly -safe` (safe mode refuses .shell and
# .system). What it would otherwise need a shell for — processes, launchd, git,
# CI, the funnel, yesterday's report — this script gathers first and hands over
# as context. The answer comes back on stdout against a JSON schema, and this
# script, not the agent, writes the file.
set -u
cd /Users/kironkp/code/moneytree || exit 1

PIPENV=/Users/kironkp/.local/bin/pipenv
CLAUDE=/Users/kironkp/.nvm/versions/node/v24.18.0/bin/claude
TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/Tailscale
DAY=$(date +%Y-%m-%d)
YESTERDAY=$(date -v-1d +%Y-%m-%d)
OUT=run/diagnosis-$DAY.json
RAW=run/diagnosis-$DAY.raw.json
CTX=run/diagnosis-context.txt
LOG=run/diagnosis.log
BUDGET_USD=2

say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "diagnosis: $*" >> "$LOG"; }

# One at a time: two overlapping runs would race on the same journal row.
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
PREV=$(ls -t run/diagnosis-*.json 2>/dev/null | grep -v '\.raw\.json$' | grep -v "$DAY" | head -1)

{
    echo "## Context gathered by deploy/daily-diagnosis.sh at $(date '+%Y-%m-%d %H:%M %Z')"
    echo; echo "### launchctl list | grep moneytree   (pid, last exit status, label)"
    launchctl list | grep moneytree
    echo; echo "### jobs disabled by an override"
    launchctl print-disabled "gui/$(id -u)" | grep moneytree || echo "none"
    echo; echo "### run/*.log, newest first"
    ls -lt run/*.log | head -40
    echo; echo "### lane agent processes and when each started"
    ps -axo pid,lstart,command | grep '[r]un_agent'
    echo; echo "### newest files under main_app/services/"
    ls -lt main_app/services/*.py main_app/services/*/*.py | head -5
    echo; echo "### locks in run/"
    ls -la run/*.lock 2>/dev/null || echo "none"
    echo; echo "### git"
    git status --short | head -20
    git log --oneline -5
    echo "unpushed:"; git log origin/main..main --oneline | head -10
    echo; echo "### CI"
    gh run list --repo kironkp/money-tree --limit 3 2>&1
    echo; echo "### funnel"
    "$TAILSCALE" funnel status 2>&1 | head -10
    echo; echo "### yesterday's evening report ($YESTERDAY)"
    "$PIPENV" run python manage.py daily_report --no-email --no-journal --date "$YESTERDAY" 2>&1 | head -300
    echo; echo "### the last recorded diagnosis (${PREV:-none})"
    [ -n "$PREV" ] && cat "$PREV"
} > "$CTX" 2>&1

# A failed rerun on the same day must not re-record this morning's earlier file.
rm -f "$OUT" "$RAW"
SID=$(uuidgen | tr 'A-Z' 'a-z')
say "session $SID, budget \$$BUDGET_USD"
# Deny rules win over any allow rule a settings file might add later, so the
# job stays read-only even if someone widens the user's permissions; only user
# settings load, and no MCP server is reachable.
{ cat deploy/daily-diagnosis-prompt.md; echo; cat "$CTX"; } | "$CLAUDE" -p \
    --setting-sources user --strict-mcp-config \
    --allowedTools Read Grep Glob "Bash(sqlite3 -readonly -safe db.sqlite3 *)" \
    --disallowedTools "Read(./.env)" Edit Write NotebookEdit Agent Workflow WebFetch WebSearch \
        "Bash(pipenv:*)" "Bash(python:*)" "Bash(python3:*)" "Bash(launchctl:*)" "Bash(git:*)" "Bash(rm:*)" \
    --permission-mode dontAsk \
    --model sonnet --effort medium --max-turns 30 --max-budget-usd "$BUDGET_USD" \
    --output-format json --json-schema "$(cat deploy/daily-diagnosis-schema.json)" \
    --session-id "$SID" > "$RAW" 2>> "$LOG"

# The schema guarantees the agent's half; this adds the ranking and counts the
# workflow used to add, so record_diagnosis reads the same shape it always has.
if ! python3 - "$RAW" "$OUT" >> "$LOG" 2>&1 <<'PY'
import json, sys
raw, out = sys.argv[1], sys.argv[2]
try:
    r = json.load(open(raw))
except Exception as exc:
    print(f'no result JSON from claude: {exc}')
    sys.exit(1)
print(f"claude: {r.get('subtype')}, {r.get('num_turns')} turns, ${r.get('total_cost_usd') or 0:.4f}, "
      f"{(r.get('duration_ms') or 0) / 1000:.0f} s")
d = r.get('structured_output')
if r.get('is_error') or not isinstance(d, dict):
    print('no structured output')
    sys.exit(1)
rank = {'critical': 0, 'warn': 1, 'note': 2}
findings = sorted(d.get('findings') or [], key=lambda f: rank.get(f.get('severity'), 3))
counts = {k: sum(1 for f in findings if f.get('severity') == k) for k in rank}
with open(out, 'w') as fh:
    json.dump({'headlines': d.get('headlines') or [], 'findings': findings, 'counts': counts}, fh, indent=2)
PY
then
    say "claude produced no usable result; the raw reply is in $RAW"
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
