#!/usr/bin/env bash
# ChatIFU Olympus / Gyrus ACMI — weekly index mirror (~16 requests to olympus-europa.com's
# Solr endpoint, 1.2s apart) joined to GUDID offline. Not a fleet loop: the resolver re-scans
# all ~7k devices each run and keeps no per-device state, so the fleet could never idle it.
# First run 2026-09-02: 2,692/6,953 devices, 3,697 rows. See resolvers/olympus_resolver.py.
set -uo pipefail
VAULT="/home/biscuited/projects/chatifu_vault"
LOG_DIR="$VAULT/runs/olympus"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/olympus_$(date +%Y%m%d_%H%M%S).log"
PY="$VAULT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$VAULT" || exit 1

"$PY" -m resolvers.olympus_resolver --apply >>"$LOG" 2>&1
status=$?
tail -1 "$LOG"
if [ "$status" -ne 0 ]; then
  "$PY" - "$LOG" <<'PY'
import sys
sys.path.insert(0, "/home/biscuited/projects/chatifu_vault")
try:
    from healthcheck import telegram_alert
    telegram_alert(f"⚠️ ChatIFU Olympus mirror failed (see {sys.argv[1]})")
except Exception as exc:
    print(f"alert failed: {exc}")
PY
fi

find "$LOG_DIR" -name 'olympus_*.log' -mtime +90 -delete 2>/dev/null
exit 0
