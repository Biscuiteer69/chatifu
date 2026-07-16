"""ChatIFU weekly improvement digest — closes the loop on the beta signals.

Turns the passive logs into an action list:
  * coverage growth (found IFU links this week, per manufacturer)
  * COVERAGE GAPS  — device searches that found nothing (what to scrape next)
  * SERVE FAILURES — /answer misses grouped by error (e.g. presigned-URL 403s)
  * ACCURACY       — 👎 feedback with notes (what to fix), and the 👍/👎 ratio
  * FLAGS          — enabled scrapers that added nothing this week (stalled/blocked)

Delivers a concise summary to Telegram and writes the full report to
runs/chatifu_digest/. Run weekly from cron.
"""
from __future__ import annotations

import collections
import json
import re
import sqlite3
import time
from pathlib import Path

from healthcheck import telegram_alert  # reuse the fail-soft Telegram sender

VAULT = Path(__file__).resolve().parent
DB = str(VAULT / "chatifu.sqlite3")
LOGS = VAULT / "logs"
OUT_DIR = VAULT / "runs" / "chatifu_digest"
WINDOW_DAYS = 7
FLEET_TARGETS = ["medtronic", "edwards", "stryker", "zimmer_biomet"]


def _load_jsonl(name: str, since: float) -> list[dict]:
    path = LOGS / name
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if float(rec.get("ts", 0)) >= since:
            rows.append(rec)
    return rows


def _norm_error(err: str) -> str:
    """Collapse an error message to a signature (drop ids/numbers) for grouping."""
    e = re.sub(r"https?://\S+", "<url>", err or "")
    e = re.sub(r"\d+", "N", e)
    return e[:80]


def _q(conn, sql: str, params=()) -> list:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def main() -> int:
    since = time.time() - WINDOW_DAYS * 86400
    conn = sqlite3.connect(DB)

    # --- Coverage --------------------------------------------------------
    total_found = _q(conn, "SELECT count(*) FROM ifu_links WHERE status='found'")
    total_found = total_found[0][0] if total_found else 0
    covered_devices = _q(conn, "SELECT count(DISTINCT catalog_number) FROM ifu_links WHERE status='found'")
    covered_devices = covered_devices[0][0] if covered_devices else 0
    week_by_fam = dict(_q(
        conn,
        "SELECT manufacturer_family, count(*) FROM ifu_links "
        "WHERE status='found' AND retrieved_at >= datetime('now', ?) "
        "GROUP BY manufacturer_family ORDER BY 2 DESC",
        (f"-{WINDOW_DAYS} days",),
    ))
    total_week = sum(week_by_fam.values())

    # --- Misses ----------------------------------------------------------
    misses = _load_jsonl("misses.jsonl", since)
    no_device = collections.Counter(
        (m.get("q") or "").strip().lower() for m in misses if m.get("kind") == "no_device")
    no_answer = [m for m in misses if m.get("kind") == "no_answer"]
    serve_errors = collections.Counter(
        _norm_error(m.get("error") or "no passage found") for m in no_answer)

    # --- Feedback --------------------------------------------------------
    feedback = _load_jsonl("feedback.jsonl", since)
    ups = sum(1 for f in feedback if f.get("verdict") == "up")
    downs = [f for f in feedback if f.get("verdict") == "down"]

    # --- Flags: enabled scrapers that added nothing this week ------------
    stalled = [t for t in FLEET_TARGETS if week_by_fam.get(t, 0) == 0
               and week_by_fam.get("johnson_and_johnson" if t == "jnj" else t, 0) == 0]

    # --- Build report ----------------------------------------------------
    L: list[str] = []
    L.append(f"ChatIFU weekly digest — {time.strftime('%Y-%m-%d')}")
    L.append(f"Coverage: {total_found:,} IFU links over {covered_devices:,} devices "
             f"(+{total_week:,} links this week)")
    if week_by_fam:
        L.append("  new this week: " + ", ".join(f"{k} +{v:,}" for k, v in list(week_by_fam.items())[:8]))
    L.append("")

    L.append(f"COVERAGE GAPS — searches with no device ({sum(no_device.values())} total):")
    for q, c in no_device.most_common(10):
        L.append(f"  {c:>3}x  {q or '(blank)'}")
    if not no_device:
        L.append("  (none)")
    L.append("")

    L.append(f"SERVE FAILURES — /answer misses ({len(no_answer)} total), by cause:")
    for sig, c in serve_errors.most_common(8):
        L.append(f"  {c:>3}x  {sig}")
    if not no_answer:
        L.append("  (none)")
    L.append("")

    total_fb = ups + len(downs)
    L.append(f"ACCURACY — feedback: 👍 {ups} / 👎 {len(downs)}"
             + (f"  ({ups * 100 // total_fb}% positive)" if total_fb else ""))
    for f in downs[:10]:
        note = f" — {f.get('note')}" if f.get("note") else ""
        L.append(f"  👎 {f.get('catalog')} p{f.get('hit_page')} · {f.get('question')}{note}")
    L.append("")

    if stalled:
        L.append(f"⚠ FLAGS: enabled scrapers with 0 new links this week: {', '.join(stalled)}")

    report = "\n".join(L)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"digest_{time.strftime('%Y%m%d')}.txt"
    out_path.write_text(report)
    print(report)
    print(f"\nwritten: {out_path}")

    # Telegram: keep under the 4096 limit.
    telegram_alert(report[:3900])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
