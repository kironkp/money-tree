#!/bin/bash
# 02:10 nightly: research every strategy (parked ones included), then restart the
# agents so anything promoted is actually traded today instead of tomorrow.
# Without the restart a promotion sits in the database while the live agent keeps
# running the parameters it read at startup.
set -u
cd /Users/kironkp/code/moneytree || exit 1
PIPENV=/Users/kironkp/.local/bin/pipenv
"$PIPENV" run python manage.py auto_research
echo "--- restarting agents so promotions take effect ---"
"$PIPENV" run python manage.py restart_agents
