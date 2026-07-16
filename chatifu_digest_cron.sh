#!/usr/bin/env bash
# ChatIFU weekly improvement digest (Mondays). Sends to Telegram, writes report.
set -uo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
VAULT="/home/biscuited/projects/chatifu_vault"
LOG_DIR="$VAULT/runs/chatifu_digest"
mkdir -p "$LOG_DIR"
PY="$VAULT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$VAULT" || exit 1
"$PY" "$VAULT/improvement_digest.py" >>"$LOG_DIR/cron.log" 2>&1
exit $?
