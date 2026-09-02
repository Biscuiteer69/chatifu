#!/usr/bin/env bash
# ChatIFU Medtronic BRAND path — daily batch of 40 brands (<=80 requests to
# manuals.medtronic.com findby=brand, 1.5s apart). Complements the fleet's per-model
# worker: MITG/Covidien devices (Shiley, Polysorb, Sherpa...) have no model on the portal
# and only resolve through their brand. Each brand searched is remembered for 30 days
# (medtronic_brand_runs) so dead brands do not head every batch; the loader drops devices
# as they gain documents, so the walk converges. First batch 2026-09-02: 12 brands,
# 2,358 devices, 17 requests. See resolvers/medtronic_resolver.py (--by-brand).
set -uo pipefail
VAULT="/home/biscuited/projects/chatifu_vault"
LOG_DIR="$VAULT/runs/medtronic_brand"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/brand_$(date +%Y%m%d_%H%M%S).log"
PY="$VAULT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$VAULT" || exit 1

"$PY" -m resolvers.medtronic_resolver --by-brand --apply --batch 40 >>"$LOG" 2>&1
status=$?
tail -1 "$LOG"
if [ "$status" -ne 0 ]; then
  "$PY" - "$LOG" "$status" <<'PY'
import sys
sys.path.insert(0, "/home/biscuited/projects/chatifu_vault")
try:
    from healthcheck import telegram_alert
    what = "portal BLOCKED the run" if sys.argv[2] == "2" else f"exit {sys.argv[2]}"
    telegram_alert(f"⚠️ ChatIFU Medtronic brand batch failed: {what} (see {sys.argv[1]})")
except Exception as exc:
    print(f"alert failed: {exc}")
PY
fi

find "$LOG_DIR" -name 'brand_*.log' -mtime +90 -delete 2>/dev/null
exit 0
