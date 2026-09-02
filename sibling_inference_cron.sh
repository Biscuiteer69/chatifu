#!/usr/bin/env bash
# ChatIFU sibling inference — daily, zero portal requests. Hands each unresolved device the
# IFU its resolved siblings already hold (same maker + brand + FDA submission, or a brand the
# portal answered unanimously for). First run 2026-09-02 wrote 10,486 rows; each night it
# picks up whatever the scrapers resolved that day. See resolvers/sibling_inference.py.
set -uo pipefail
VAULT="/home/biscuited/projects/chatifu_vault"
LOG_DIR="$VAULT/runs/sibling"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/sibling_$(date +%Y%m%d_%H%M%S).log"
PY="$VAULT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$VAULT" || exit 1

"$PY" -m resolvers.sibling_inference --apply >>"$LOG" 2>&1
status=$?
tail -1 "$LOG"
if [ "$status" -ne 0 ]; then
  "$PY" - "$LOG" <<'PY'
import sys
sys.path.insert(0, "/home/biscuited/projects/chatifu_vault")
try:
    from healthcheck import telegram_alert
    telegram_alert(f"⚠️ ChatIFU sibling inference failed (see {sys.argv[1]})")
except Exception as exc:
    print(f"alert failed: {exc}")
PY
fi

find "$LOG_DIR" -name 'sibling_*.log' -mtime +30 -delete 2>/dev/null
exit 0
