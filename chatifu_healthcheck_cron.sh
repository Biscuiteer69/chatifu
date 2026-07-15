#!/usr/bin/env bash
# ChatIFU health-check (every 30 min). Fires a Telegram alarm on any failure.
set -uo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"   # needed for `systemctl --user`
VAULT="/home/biscuited/projects/chatifu_vault"
LOG_DIR="$VAULT/runs/chatifu_health"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/health_$(date +%Y%m%d_%H%M%S).log"
PY="$VAULT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$VAULT" || exit 1
"$PY" "$VAULT/healthcheck.py" >>"$LOG" 2>&1
status=$?
echo "exit=$status" >>"$LOG"
find "$LOG_DIR" -name 'health_*.log' -mtime +14 -delete 2>/dev/null
exit $status
