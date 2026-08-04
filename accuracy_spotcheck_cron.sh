#!/usr/bin/env bash
# ChatIFU answer-quality spot-check — daily. Alerts on Telegram only when the FLAGGED rate
# (confident-but-off-topic highlights) crosses the threshold, which is the failure this product
# most needs to avoid: an authoritative-looking passage that does not answer the question asked.
#
# Coverage monitoring (scrape_goal_monitor_cron.sh) answers "do we hold a document?".
# This answers "does the document we serve actually address the question?" — measured 27%
# FLAGGED on first run, so it needs watching, not assuming.
set -uo pipefail
VAULT="/home/biscuited/projects/chatifu_vault"
LOG_DIR="$VAULT/runs/accuracy"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/accuracy_$(date +%Y%m%d_%H%M%S).log"
PY="$VAULT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cd "$VAULT" || exit 1

"$PY" "$VAULT/accuracy_spotcheck.py" --per-family 3 >>"$LOG" 2>&1
status=$?

# Parse the summary the script prints; alert only on a threshold breach so this stays quiet
# when healthy, the same discipline as the scrape monitor.
"$PY" - "$LOG" <<'PY'
import re, sys, pathlib
text = pathlib.Path(sys.argv[1]).read_text(errors="replace")
def grab(pattern, cast=int, default=None):
    m = re.search(pattern, text)
    return cast(m.group(1)) if m else default
probes  = grab(r"probes:\s+(\d+)")
hits    = grab(r"returned hit:\s+(\d+)")
flagged = grab(r"FLAGGED wrong:\s+(\d+)")
rel     = grab(r"relevant:\s+(\d+)/", int)
if probes is None or flagged is None:
    print("spotcheck produced no summary — treating as failure")
    body = f"⚠️ ChatIFU accuracy spot-check produced no summary (see {sys.argv[1]})"
else:
    pct = 100.0 * flagged / hits if hits else 0.0
    line = (f"probes {probes} | hits {hits} | relevant {rel}/{hits} | "
            f"FLAGGED {flagged} ({pct:.0f}% of hits)")
    print(line)
    # 20% is the line: at that rate a clinician meets a confidently-wrong passage in one
    # of five answers, which is not shippable however good coverage looks.
    body = (f"⚠️ ChatIFU answer quality: {line}\n\nFLAGGED = a confident highlight that does "
            f"NOT answer the question asked.") if pct >= 20 else None
if body:
    sys.path.insert(0, "/home/biscuited/projects/chatifu_vault")
    try:
        from healthcheck import telegram_alert
        telegram_alert(body)
    except Exception as exc:
        print(f"alert failed: {exc}")
PY

find "$LOG_DIR" -name 'accuracy_*.log' -mtime +30 -delete 2>/dev/null
exit 0
