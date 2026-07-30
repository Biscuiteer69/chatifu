#!/usr/bin/env bash
# Top-20 IFU scrape goal monitor — every 6h. Telegram fires only when something is wrong.
set -uo pipefail
VAULT="/home/biscuited/projects/chatifu_vault"
LOG_DIR="$VAULT/runs/scrape_goal"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/goal_$(date +%Y%m%d_%H%M%S).log"
PY="$VAULT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$VAULT" || exit 1
"$PY" "$VAULT/scrape_goal_monitor.py" >>"$LOG" 2>&1
status=$?
echo "exit=$status" >>"$LOG"
find "$LOG_DIR" -name 'goal_*.log' -mtime +30 -delete 2>/dev/null
exit 0   # never let a monitor failure mark the cron job as failing
