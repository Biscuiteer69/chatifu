#!/usr/bin/env bash
# ChatIFU FDA PMA approved-labeling sweep — weekly. accessdata.fda.gov serves the
# FDA-approved labeling (the real IFU at approval revision) as P######C.pdf next to the
# SSED, and every PMA device that no maker portal has answered for is linked to it as a
# found row (fda_pma_labeling, ranked below every portal tier). The first full pass
# (2026-09-02) HEADed 3,446 (PMA, supplement) pairs and found 1,342 documents covering
# ~46k devices; after that the pending set is only what new GUDID loads and new
# supplements add, so 500 HEADs a week at 1/s is plenty. See resolvers/fda_resolver.py.
set -uo pipefail
VAULT="/home/biscuited/projects/chatifu_vault"
LOG_DIR="$VAULT/runs/fda_pma_labeling"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/labeling_$(date +%Y%m%d_%H%M%S).log"
PY="$VAULT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$VAULT" || exit 1

"$PY" -m resolvers.fda_resolver --pma-labeling 500 >>"$LOG" 2>&1
status=$?
tail -1 "$LOG"
if [ "$status" -ne 0 ]; then
  "$PY" - "$LOG" "$status" <<'PY'
import sys
sys.path.insert(0, "/home/biscuited/projects/chatifu_vault")
try:
    from healthcheck import telegram_alert
    telegram_alert(f"⚠️ ChatIFU FDA PMA labeling sweep failed: exit {sys.argv[2]} (see {sys.argv[1]})")
except Exception as exc:
    print(f"alert failed: {exc}")
PY
fi

find "$LOG_DIR" -name 'labeling_*.log' -mtime +120 -delete 2>/dev/null
exit 0
